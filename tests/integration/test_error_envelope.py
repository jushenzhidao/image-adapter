"""The OpenAI error envelope has to hold on every exit, not only on the ones a
handler raises.

This is the regression guard for a real gap: 404 and 405 used to come straight
from Starlette's router as ``PlainTextResponse``, so a client doing
``response.json()`` got a parse error instead of an error message. The old
``@handle_errors`` decorator could never cover them, because a decorator only
wraps the callable it decorates and the router fails before that callable runs.
"""

from __future__ import annotations


def assert_openai_envelope(resp, code: str) -> None:
    """Every error body is {"error": {message, type, param, code}}."""
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert error["type"]
    assert "param" in error


def test_unknown_path_is_an_openai_404(client):
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    assert_openai_envelope(resp, "not_found")


def test_wrong_method_is_an_openai_405(client):
    resp = client.get("/v1/images/generations")
    assert resp.status_code == 405
    assert_openai_envelope(resp, "method_not_allowed")


def test_health_wrong_method_is_an_openai_405(client):
    resp = client.delete("/health")
    assert resp.status_code == 405
    assert_openai_envelope(resp, "method_not_allowed")


def test_missing_adapter_key_is_an_openai_401(client):
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 401
    assert_openai_envelope(resp, "invalid_adapter_key")


def test_channel_config_error_keeps_its_code(client):
    """Missing X-Upstream-Url is reported by channel.py, not by validation, so
    the OpenAI envelope and the specific code both survive."""
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "x"},
        headers={
            "X-Adapter-Key": "test-adapter-key",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert_openai_envelope(resp, "channel_config_error")
    assert resp.json()["error"]["param"] == "X-Upstream-Url"


def test_errors_carry_a_request_id(client):
    resp = client.get("/definitely-not-a-route")
    assert resp.headers.get("X-Request-Id")


def test_docs_route_is_served(client):
    """The migration declares the channel contract via Header(), which only
    pays off if the generated schema is actually reachable."""
    schema = client.get("/openapi.json").json()
    assert "/v1/images/generations" in schema["paths"]
    # Every channel header is declared optional, so a missing one can never
    # surface as a FastAPI 422 ahead of channel.py's own error code.
    params = schema["paths"]["/v1/images/generations"]["post"]["parameters"]
    names = {p["name"] for p in params}
    assert {"X-Upstream-Url", "X-Adapter-Key", "X-Script"} <= names
    assert all(p.get("required") is not True for p in params)
