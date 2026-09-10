"""Integration tests for the middleware stack: request ids, CORS preflight and
rate limiting (AC-19).

These exercise the raw-ASGI implementations in adapter/middleware/. Two tests
previously here were dropped rather than updated:
``test_auth_disabled_by_default`` posted to ``/v1/images`` -- a route that has
never existed -- and asserted only ``status != 401``, so it passed whatever the
service did, and it named ADAPTER_AUTH_ENABLED, a config key that does not
exist (the real fields are adapter_key / adapter_key_required). Admission is
covered properly in test_error_envelope.py.
"""

from __future__ import annotations

import re


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_request_id_is_minted_when_absent(client):
    resp = client.get("/health")
    assert len(resp.headers["X-Request-Id"]) == 36


def test_request_id_from_the_caller_is_honoured(client):
    """The control plane correlates its logs with ours, so an id it supplied
    must survive."""
    resp = client.get("/health", headers={"X-Request-Id": "caller-supplied-id"})
    assert resp.headers["X-Request-Id"] == "caller-supplied-id"


def test_cors_preflight_allows_the_channel_headers(client):
    """A preflight that does not list the channel headers makes the whole
    contract unusable from a browser."""
    resp = client.options(
        "/v1/images/generations",
        headers={
            "Origin": "https://console.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-adapter-key,x-upstream-url",
        },
    )
    assert resp.status_code == 200
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "x-adapter-key" in allowed
    assert "x-upstream-url" in allowed


def _force_limit(monkeypatch, per_minute: int) -> None:
    from adapter.middleware import rate_limit
    from adapter.settings import Settings

    strict = Settings(
        rate_limit_enabled=True, rate_limit_per_minute=per_minute, redis_url=""
    )
    monkeypatch.setattr(rate_limit, "get_settings", lambda: strict)
    rate_limit._local_counters.clear()


def test_rate_limit_rejects_with_the_openai_envelope(client, monkeypatch):
    from adapter.middleware import rate_limit

    _force_limit(monkeypatch, per_minute=1)
    body = {"prompt": "x"}
    headers = {"X-Adapter-Key": "test-adapter-key"}

    first = client.post("/v1/images/generations", json=body, headers=headers)
    second = client.post("/v1/images/generations", json=body, headers=headers)
    rate_limit._local_counters.clear()

    # The first request is allowed through to the pipeline (and fails on the
    # missing channel headers); the second must be rejected by the limiter.
    assert first.status_code != 429
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"


def test_rate_limit_never_blocks_health(client, monkeypatch):
    """Orchestrator probes must not count against the ceiling, or a busy
    service would report itself unhealthy."""
    _force_limit(monkeypatch, per_minute=0)
    for _ in range(3):
        assert client.get("/health").status_code == 200


def test_rate_limit_key_carries_no_credential(client, monkeypatch):
    """The limiter buckets by Authorization, and that value IS the upstream
    vendor's credential. Using it as a Redis key name would publish the
    credential into KEYS/SCAN, MONITOR, the slow log and every RDB dump, so the
    key must be a digest."""
    from adapter.middleware import rate_limit

    _force_limit(monkeypatch, per_minute=100)
    client.post(
        "/v1/images/generations",
        json={"prompt": "x"},
        headers={
            "X-Adapter-Key": "test-adapter-key",
            "Authorization": "Bearer ark-SUPERSECRET-abcdef",
        },
    )

    keys = list(rate_limit._local_counters)
    rate_limit._local_counters.clear()

    assert keys, "the request should have been counted"
    for key in keys:
        assert "SUPERSECRET" not in key
        assert "Bearer" not in key
        assert re.fullmatch(r"ratelimit:[0-9a-f]{16}:\d+", key), key
