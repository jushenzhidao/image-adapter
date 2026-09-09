"""Integration test for GET /health endpoint (AC-18)."""

import pytest


def test_health_endpoint(client):
    """AC-18: /health returns 200 with dep status."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "deps" in data
    # In test env (no Redis/MinIO), deps are degraded
    assert "redis" in data["deps"]
    assert "minio" in data["deps"]
