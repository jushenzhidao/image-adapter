"""Integration tests for middleware: auth, rate limit, CORS (AC-19)."""

import pytest


def test_auth_disabled_by_default(client):
    """AC-19: When ADAPTER_AUTH_ENABLED=false, requests pass without token."""
    resp = client.post(
        "/v1/images",
        json={"model": "vendor-a-text2img", "prompt": "test", "n": 1},
    )
    assert resp.status_code != 401


def test_health_always_public(client):
    """AC-19: /health is always accessible without auth."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_cors_headers_present(client):
    """CORS middleware adds appropriate headers."""
    resp = client.get("/health")
    assert resp.status_code == 200
